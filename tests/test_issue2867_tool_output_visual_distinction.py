from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_tool_cards_render_persistent_tool_output_badge():
    """Tool output must remain distinguishable without relying on hover state."""
    build_start = UI_JS.index('function buildToolCard(tc){')
    build_end = UI_JS.index('function _syncToolCallGroupSummary', build_start)
    build_tool_card = UI_JS[build_start:build_end]

    assert '<span class="tool-card-badge">Tool output</span>' in build_tool_card
    assert '<span class="tool-card-name">${esc(displayName)}</span>' in build_tool_card
    assert build_tool_card.index('class="tool-card-badge"') < build_tool_card.index('class="tool-card-name"')


def test_tool_card_badge_style_is_not_hover_only():
    badge_rule_start = STYLE_CSS.index('.tool-card-badge{')
    badge_rule_end = STYLE_CSS.index('}', badge_rule_start)
    badge_rule = STYLE_CSS[badge_rule_start:badge_rule_end]

    assert '.tool-card:hover .tool-card-badge' not in STYLE_CSS
    assert 'text-transform:uppercase' in badge_rule
    assert 'background:var(--accent-bg)' in badge_rule
    assert 'border:1px solid var(--accent-bg-strong)' in badge_rule
    assert 'color:var(--accent-text)' in badge_rule


def test_tool_cards_have_persistent_accent_rail():
    assert '.tool-card{background:var(--surface-subtle);' in STYLE_CSS
    assert 'border-left:3px solid var(--accent-bg-strong)' in STYLE_CSS
