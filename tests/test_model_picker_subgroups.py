from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
UI=(ROOT/"static"/"ui.js").read_text(encoding="utf-8")
CSS=(ROOT/"static"/"style.css").read_text(encoding="utf-8")

def test_vendor_subgroup_allowlist():
    assert "SUB_GROUP_PROVIDERS" in UI
    assert "openrouter" in UI
    assert "nous" in UI

def test_vendor_prefix_strips_routing_prefix():
    # The strip pattern must match the existing normalizer at line 1749
    assert "replace(/^@([^:]+:)+/,'')" in UI
    assert re.search(r"indexOf\('/'\)|split\('/'\)", UI)

def test_subgroup_keys_use_double_colon_separator():
    assert "::" in UI
    assert re.search(r"_groupOpenState\[subKey\]", UI)

def test_single_model_vendor_buckets_stay_flat():
    # pfxRows.length>=2 gate means single-model prefixes render flat
    assert re.search(r"\.length\s*>=\s*2", UI)

def test_visible_rows_walk_all_group_body_ancestors():
    visible_match=re.search(r"const _visibleModelRows=\(\)=>[\s\S]+?\}\);", UI)
    assert visible_match, "_visibleModelRows definition not found"
    visible=visible_match.group(0)
    assert "while(" in visible
    assert "parentElement" in visible
    assert "model-group-body" in visible
    assert "closest('.model-group-body')" not in visible

def test_nested_group_css_exists():
    assert ".model-group.sub" in CSS
    assert ".model-group-body.sub" in CSS
