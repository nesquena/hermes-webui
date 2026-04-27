import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_messages_zero_skips_effective_model_resolution():
    src = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")

    assert re.search(
        r"effective_model\s*=\s*\(\s*"
        r"_resolve_effective_session_model_for_display\(s\)\s*"
        r"if resolve_model\s*else None\s*\)",
        src,
    ), "messages=0 metadata requests must not resolve the model catalog"
    assert 'resolve_model_default = "1" if load_messages else "0"' in src


def test_full_message_load_updates_viewed_count_after_metadata_fast_path():
    src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "_setSessionViewedCount(S.session.session_id, Number(data.session.message_count || 0));" in src
    assert "_setSessionViewedCount(sid, Number(S.session.message_count || msgs.length));" in src


def test_lazy_message_load_skips_model_resolution():
    src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "messages=1&resolve_model=0" in src


def test_session_switch_defers_model_resolution_without_blocking():
    src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert "messages=0&resolve_model=0" in src
    assert "function _resolveSessionModelForDisplaySoon" in src
    assert "messages=0&resolve_model=1" in src
    assert "_modelResolutionDeferred=true" in src
    assert "deferModelCorrection" in ui
    # The persist guard combines deferModelCorrection with liveFetchPending (#1169).
    # Accept either the original single-condition form or the combined form.
    assert (
        "if(!deferModelCorrection)" in ui
        or "if(!deferModelCorrection && !liveFetchPending)" in ui
    ), "syncTopbar must gate the model-correction persist on deferModelCorrection"


def test_boot_does_not_block_session_restore_on_model_catalog():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")

    assert "if(s.default_model) window._defaultModel=s.default_model;" in src
    assert "const _modelDropdownReady=populateModelDropdown().then" in src
    assert "window._modelDropdownReady=_modelDropdownReady" in src
    assert "await populateModelDropdown()" not in src
