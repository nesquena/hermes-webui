"""
Regression test for #1189 — openai-codex provider group should appear
in the model picker when OPENAI_API_KEY is configured.

The env-var detection block in ``api/config.py`` previously mapped
``OPENAI_API_KEY`` to only the ``openai`` provider group; the
``openai-codex`` group has its own static model list in
``_PROVIDER_MODELS`` (9 models: gpt-5.5, gpt-5.4, codex-specific
variants, etc.) but no automatic detection path.

Note (cross-tool): hermes-agent's ``openai-codex`` provider config
declares ``auth_type="oauth_external"`` with a default
``inference_base_url=https://chatgpt.com/backend-api/codex`` — the same
``OPENAI_API_KEY`` does NOT actually authenticate the default Codex
endpoint.  Users without an OAuth state will see codex models in the
picker but hit auth errors at use time.  The fix is still net-positive
UX (no manual config.yaml edit needed for users who DO have both), but
the simple detect-on-OPENAI_API_KEY shortcut is documented here as a
known limitation.
"""
import pathlib

import api.config as config

REPO = pathlib.Path(__file__).parent.parent
CONFIG_SRC = (REPO / "api" / "config.py").read_text(encoding="utf-8")


def test_openai_api_key_detection_block_includes_openai_codex():
    """Source assertion: the env-var detection block in
    ``get_available_models`` adds both ``openai`` and ``openai-codex``
    when ``OPENAI_API_KEY`` is set."""
    # Find the OPENAI_API_KEY detection block
    idx = CONFIG_SRC.find('all_env.get("OPENAI_API_KEY")')
    assert idx >= 0, (
        "Could not locate the OPENAI_API_KEY env-var detection block in "
        "api/config.py"
    )
    # Look at the next ~400 chars for the .add() calls
    block = CONFIG_SRC[idx:idx + 400]
    assert 'detected_providers.add("openai")' in block, (
        "OPENAI_API_KEY block must add the 'openai' provider"
    )
    assert 'detected_providers.add("openai-codex")' in block, (
        "OPENAI_API_KEY block must add the 'openai-codex' provider so the "
        "Codex group appears in the picker without manual config.yaml edit "
        "(#1189). The two providers share the same OPENAI_API_KEY by "
        "convention even though hermes-agent's openai-codex default uses "
        "OAuth — a config.yaml override or manual OAuth state is needed for "
        "the picker selection to actually work."
    )


def test_openai_codex_static_model_list_present():
    """Sanity: the openai-codex provider has a non-empty static model list
    in _PROVIDER_MODELS so adding it to detected_providers actually
    surfaces models in the picker rather than an empty group."""
    assert "openai-codex" in config._PROVIDER_MODELS, (
        "_PROVIDER_MODELS must include 'openai-codex' for the detection "
        "fix to surface anything"
    )
    models = config._PROVIDER_MODELS["openai-codex"]
    assert len(models) > 0, "openai-codex must have at least one static model"
    # Sanity: contains codex-specific variants as well as shared gpt-5.x
    ids = {m["id"] for m in models}
    assert any("codex" in mid for mid in ids), (
        "openai-codex group should expose at least one codex-specific model "
        "(otherwise it's redundant with the openai group)"
    )


def test_openai_codex_display_name_present():
    """The Codex group needs a human-readable label in _PROVIDER_DISPLAY,
    otherwise the picker falls back to the raw provider id."""
    assert config._PROVIDER_DISPLAY.get("openai-codex"), (
        "_PROVIDER_DISPLAY must have a label for 'openai-codex'"
    )
