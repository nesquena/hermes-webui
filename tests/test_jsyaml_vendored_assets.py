"""Regression coverage for vendored js-yaml assets."""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
VENDOR_FILE = REPO / "static" / "vendor" / "js-yaml" / "4.1.0" / "js-yaml.min.js"


def test_runtime_loads_vendored_jsyaml_instead_of_cdnjs():
    assert "static/vendor/js-yaml/4.1.0/js-yaml.min.js" in UI_JS
    assert "https://cdnjs.cloudflare.com/ajax/libs/js-yaml/4.1.0/js-yaml.min.js" not in UI_JS


def test_vendored_jsyaml_asset_is_present():
    assert VENDOR_FILE.is_file()
    content = VENDOR_FILE.read_text(encoding="utf-8")
    assert "jsyaml" in content
