from pathlib import Path

from api import config


def _models_with_cfg(model_cfg=None, fallback_providers=None, custom_providers=None, active_provider=None):
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
            "providers": custom_providers or {},
        }
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


def test_get_available_models_cache_preserves_configured_model_badges(tmp_path, monkeypatch):
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
        assert cold.get("configured_model_badges", {}).get("@copilot:gpt-4.1", {}).get("label") == "Fallback 1"

        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        warm = config.get_available_models()

        assert "configured_model_badges" in warm, (
            "O cache persistido de /api/models não pode descartar configured_model_badges, "
            "senão o deploy/servidor reiniciado perde as TAGS do dropdown mesmo com o código novo."
        )
        assert warm["configured_model_badges"].get("@copilot:gpt-4.1", {}).get("label") == "Fallback 1"
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
    assert 'id="composerModelBadge"' in html, (
        "O chip principal do modelo precisa de um container dedicado para exibir o badge "
        "do modelo selecionado fora do dropdown."
    )
    assert "composer-model-badge" in css, (
        "O badge do chip principal precisa de estilo próprio para ficar visível ao lado "
        "do nome do modelo selecionado."
    )
    assert "const badge=_getConfiguredModelBadge(sel.value||'',window._configuredModelBadges||{});" in js, (
        "syncModelChip() deve buscar o badge configurado do modelo selecionado e projetá-lo "
        "no chip principal da composer."
    )
    assert "badgeEl.textContent=badge&&badge.label?badge.label:'';" in js, (
        "syncModelChip() deve preencher o texto do badge visível no chip principal quando houver metadata configurada."
    )
