from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text()


def test_temporary_configured_model_option_carries_provider_badge():
    """Configured picker rows that are not already <option>s must keep provider."""

    assert "const badge=(window._configuredModelBadges||{})[value];" in UI_JS
    assert "if(badge&&badge.provider) opt.dataset.provider=badge.provider;" in UI_JS


def test_model_state_reads_provider_from_option_dataset_before_optgroup():
    """selectModelFromDropdown() adds temporary options outside optgroups."""

    start = UI_JS.index("function _getOptionProviderId(opt)")
    body = UI_JS[start : UI_JS.index("function _providerFromModelValue", start)]
    assert "if(opt.dataset && opt.dataset.provider) return opt.dataset.provider;" in body
    assert body.index("opt.dataset && opt.dataset.provider") < body.index("const group=opt.parentElement")