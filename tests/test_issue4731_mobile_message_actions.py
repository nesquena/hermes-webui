"""Static regression coverage for the mobile message action touch-target fix."""
from pathlib import Path


STYLE_CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")


def _mobile_block():
    marker = "@media(max-width:640px){"
    start = STYLE_CSS.index(marker)
    end = STYLE_CSS.index("/* ── /btw ephemeral side-question bubble ── */", start)
    return STYLE_CSS[start:end]


def test_mobile_block_enlarges_msg_action_buttons():
    block = _mobile_block()
    assert ".msg-action-btn{min-width:40px;min-height:40px;padding:8px;display:inline-flex;align-items:center;justify-content:center;}" in block


def test_mobile_block_widens_msg_actions_gap():
    block = _mobile_block()
    assert ".msg-actions{gap:4px;}" in block


def test_mobile_block_keeps_visibility_override():
    block = _mobile_block()
    assert ".msg-actions{opacity:1;}" in block


def test_desktop_base_rules_remain_small_and_untouched():
    assert ".msg-actions{display:flex;align-items:center;gap:2px;" in STYLE_CSS
    assert ".msg-action-btn{background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:2px 5px;" in STYLE_CSS
