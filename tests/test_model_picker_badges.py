from pathlib import Path

from api import config


def _models_with_cfg(
    model_cfg=None, fallback_providers=None, custom_providers=None, active_provider=None
):
    old_cfg = config.cfg
    old_mtime = config._cfg_mtime
    old_cache = config._available_models_cache
    old_cache_ts = config._available_models_cache_ts
    try:
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._cfg_mtime = 0.0
        config.cfg = {
            "model": model_cfg or {"provider": "openai-codex", "default": "gpt-5.4"},
            "fallback_providers": fallback_providers or [],
            "providers": {},
        }
        if custom_providers is not None:
            config.cfg["custom_providers"] = custom_providers
        if active_provider:
            config.cfg["model"]["provider"] = active_provider
        return config.get_available_models()
    finally:
        config.cfg = old_cfg
        config._cfg_mtime = old_mtime
        config._available_models_cache = old_cache
        config._available_models_cache_ts = old_cache_ts


def test_available_models_exposes_primary_and_fallback_badges():
    result = _models_with_cfg(
        model_cfg={"provider": "openai-codex", "default": "gpt-5.4"},
        fallback_providers=[
            {"provider": "copilot", "model": "gpt-4.1"},
            {"provider": "anthropic", "model": "claude-haiku-4.5"},
        ],
    )

    badges = result.get("configured_model_badges")
    assert isinstance(badges, dict), (
        "get_available_models() deve expor configured_model_badges para o frontend "
        "marcar visualmente o dropdown com a cadeia primária + fallback configurada."
    )
    assert badges.get("@openai-codex:gpt-5.4", {}).get("role") == "primary"
    assert badges.get("@openai-codex:gpt-5.4", {}).get("label") == "Primary"
    assert badges.get("@copilot:gpt-4.1", {}).get("role") == "fallback"
    assert badges.get("@copilot:gpt-4.1", {}).get("label") == "Fallback 1"
    assert badges.get("anthropic/claude-haiku-4.5", {}).get("role") == "fallback"
    assert badges.get("anthropic/claude-haiku-4.5", {}).get("label") == "Fallback 2"


def test_duplicate_slash_id_primary_badge_sticks_to_matching_provider_only():
    import textwrap

    root = Path(__file__).resolve().parent.parent
    src = (root / "api" / "config.py").read_text(encoding="utf-8")
    start = src.index(
        "def _build_configured_model_badges() -> dict[str, dict[str, str]]:"
    )
    end = src.index("            return badges", start) + len(
        "            return badges"
    )
    fn_src = textwrap.dedent(src[start:end])

    scope = {
        "active_provider": "custom:beta",
        "default_model": "google/gemma-4-27b",
        "cfg": {"fallback_providers": []},
        "groups": [
            {
                "provider": "Alpha",
                "provider_id": "custom:alpha",
                "models": [{"id": "google/gemma-4-27b"}],
            },
            {
                "provider": "Beta",
                "provider_id": "custom:beta",
                "models": [{"id": "@custom:beta:google/gemma-4-27b"}],
            },
        ],
        "_resolve_provider_alias": lambda provider: provider,
    }
    exec(
        "def _norm_model_id(model_id):\n"
        "    s=str(model_id or '').strip().lower()\n"
        "    if s.startswith('@') and ':' in s: s=s.split(':',1)[1]\n"
        "    if '/' in s: s=s.split('/',1)[1]\n"
        "    return s.replace('-', '.')\n",
        scope,
    )
    exec(fn_src, scope)

    badges = scope["_build_configured_model_badges"]()
    assert badges.get("@custom:beta:google/gemma-4-27b", {}).get("role") == "primary"
    assert "google/gemma-4-27b" not in badges, (
        "When duplicate slash-qualified IDs are deduplicated across providers, "
        "the shared raw ID must not keep the PRIMARY badge for the wrong provider."
    )


def test_ui_badge_lookup_prefers_row_provider_for_duplicate_model_ids():
    root = Path(__file__).resolve().parent.parent
    js = (root / "static" / "ui.js").read_text(encoding="utf-8")

    assert "function _getConfiguredModelBadge(modelId,badgeMap,providerId){" in js
    assert "child.dataset&&child.dataset.provider?child.dataset.provider:''" in js
    assert (
        "const providerMatch=matches.find(badge=>String(badge&&badge.provider||'').toLowerCase()===provider);"
        in js
    )


def test_configured_model_group_label_has_i18n_key():
    """The Configured model group must not render the raw i18n key."""
    root = Path(__file__).resolve().parent.parent
    i18n = (root / "static" / "i18n.js").read_text(encoding="utf-8")

    locale_count = i18n.count("_lang:")
    key_count = i18n.count("model_group_configured:")
    assert key_count == locale_count, (
        "model_group_configured must be present in every locale block so "
        "t('model_group_configured') never falls back to the raw key."
    )


def test_get_available_models_cache_preserves_configured_model_badges(
    tmp_path, monkeypatch
):
    cache_path = tmp_path / "models_cache.json"
    old_cfg = config.cfg
    old_mtime = config._cfg_mtime
    old_cache = config._available_models_cache
    old_cache_ts = config._available_models_cache_ts
    old_cache_path = config._models_cache_path
    try:
        monkeypatch.setattr(config, "_models_cache_path", cache_path)
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._cfg_mtime = 0.0
        config.cfg = {
            "model": {"provider": "openai-codex", "default": "gpt-5.4"},
            "fallback_providers": [{"provider": "copilot", "model": "gpt-4.1"}],
            "providers": {},
        }

        cold = config.get_available_models()
        assert (
            cold.get("configured_model_badges", {})
            .get("@copilot:gpt-4.1", {})
            .get("label")
            == "Fallback 1"
        )

        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        warm = config.get_available_models()

        assert "configured_model_badges" in warm, (
            "O cache persistido de /api/models não pode descartar configured_model_badges, "
            "senão o deploy/servidor reiniciado perde as TAGS do dropdown mesmo com o código novo."
        )
        assert (
            warm["configured_model_badges"].get("@copilot:gpt-4.1", {}).get("label")
            == "Fallback 1"
        )
    finally:
        config.cfg = old_cfg
        config._cfg_mtime = old_mtime
        config._available_models_cache = old_cache
        config._available_models_cache_ts = old_cache_ts
        monkeypatch.setattr(config, "_models_cache_path", old_cache_path)


def test_ui_renders_model_badges_from_api_payload():
    root = Path(__file__).resolve().parent.parent
    js = (root / "static" / "ui.js").read_text(encoding="utf-8")
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    css = (root / "static" / "style.css").read_text(encoding="utf-8")

    assert "window._configuredModelBadges=data.configured_model_badges||{};" in js, (
        "populateModelDropdown() deve guardar configured_model_badges do /api/models "
        "para que o dropdown reflita a cadeia configurada atual."
    )
    assert "model-opt-badge" in js, (
        "renderModelDropdown() deve renderizar um badge visual por modelo quando houver "
        "metadata de primário/fallback no payload."
    )
    assert "_getConfiguredModelBadge" in js, (
        "A UI precisa de um helper de matching resiliente para religar badges mesmo quando "
        "o update do catálogo mudar prefixos/formas do model ID."
    )
    assert "model_group_configured" in js, (
        "renderModelDropdown() deve expor uma seção Configured no topo para destacar a "
        "cadeia primária + fallback antes dos providers completos."
    )
    assert "configuredRank" in js, (
        "A UI deve calcular uma prioridade estável (primary -> fallback 1 -> fallback N) "
        "para renderizar os modelos configurados no topo do dropdown."
    )
    assert (
        "Object.entries(_badgeMap)" in js
        and "_normalizeConfiguredModelKey(existing.value)" in js
    ), (
        "renderModelDropdown() deve sintetizar entradas para modelos configurados ausentes "
        "do catálogo atual, senão fallbacks locais/Ollama desaparecem da seção Configured."
    )
    # Chip-projected badge was removed in v0.50.243 (added too much width to the
    # composer chip; signal value low since the model name is right next to it).
    # Badges remain in the dropdown rows (model-opt-badge) for picker rows.
    assert 'id="composerModelBadge"' not in html, (
        "composer-model-badge chip projection was intentionally removed — "
        "do not re-add it to the composer chip."
    )
    assert "composer-model-badge" not in css, (
        "composer-model-badge CSS was intentionally removed alongside the chip span."
    )
    assert "composerModelBadge" not in js, (
        "syncModelChip() must not reference composerModelBadge — the chip-projected "
        "badge was removed because it added too much width to the composer chip."
    )
